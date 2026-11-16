import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import load_overrides
from app.agent.registry import get_tool_registry
from app.db import get_session
from app.errors import AppError
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowVersion,
)
from app.permissions import admin_access, viewer_access
from app.services.audit import record_best_effort
from app.skills.registry import get_registry as get_skill_registry
from app.workflow.templates import TEMPLATES, get_template
from app.workflow.validate import (
    ValidationResult,
    check_structure,
    validate_publish,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])

ACTIVE_RUN_STATUSES = ("queued", "running", "waiting_approval")


class ScheduleOut(BaseModel):
    id: str
    workflow_version_id: str
    cadence: dict
    inputs: dict
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None


class LastRunOut(BaseModel):
    id: str
    status: str
    created_at: datetime | None


class WorkflowOut(BaseModel):
    id: str
    name: str
    description: str
    created_by: str
    draft_revision: int
    has_unpublished: bool
    latest_version: int | None
    next_run_at: datetime | None
    last_run: LastRunOut | None
    created_at: datetime | None


class VersionOut(BaseModel):
    id: str
    version: int
    source_revision: int
    published_by: str
    published_at: datetime
    note: str


class VersionDetailOut(VersionOut):
    graph: dict


class WorkflowDetailOut(WorkflowOut):
    draft_graph: dict | None
    latest_published: VersionOut | None
    schedules: list[ScheduleOut]


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    graph: dict | None = None


class UpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    draft_graph: dict | None = None


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int
    note: str = Field(default="", max_length=500)


class TemplateOut(BaseModel):
    key: str
    name: str
    description: str
    parameters: list[dict]


class InstantiateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)


class ValidateOut(BaseModel):
    ok: bool
    issues: list[dict]


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise AppError("workflow_not_found", "工作流不存在", 404) from None


async def _owned(session: AsyncSession, raw: str, user: User) -> Workflow:
    row = (
        await session.execute(
            select(Workflow).where(
                Workflow.id == _parse_id(raw),
                Workflow.tenant_id == user.tenant_id,
                Workflow.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "工作流不存在", 404)
    return row


async def _latest_version(
    session: AsyncSession, workflow_id: uuid.UUID
) -> WorkflowVersion | None:
    return (
        await session.execute(
            select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _version_out(row: WorkflowVersion) -> VersionOut:
    return VersionOut(
        id=str(row.id),
        version=row.version,
        source_revision=row.source_revision,
        published_by=str(row.published_by),
        published_at=row.published_at,
        note=row.note,
    )


def _schedule_out(row: WorkflowSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=str(row.id),
        workflow_version_id=str(row.workflow_version_id),
        cadence=row.cadence or {},
        inputs=row.inputs or {},
        enabled=row.enabled,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
    )


async def _next_run_at(
    session: AsyncSession, workflow_id: uuid.UUID
) -> datetime | None:
    return (
        await session.execute(
            select(func.min(WorkflowSchedule.next_run_at)).where(
                WorkflowSchedule.workflow_id == workflow_id,
                WorkflowSchedule.enabled.is_(True),
            )
        )
    ).scalar_one()


async def _last_run(
    session: AsyncSession, workflow_id: uuid.UUID
) -> WorkflowRun | None:
    return (
        await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id)
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _summary(session: AsyncSession, row: Workflow) -> WorkflowOut:
    latest = await _latest_version(session, row.id)
    last = await _last_run(session, row.id)
    return WorkflowOut(
        id=str(row.id),
        name=row.name,
        description=row.description,
        created_by=str(row.created_by),
        draft_revision=row.draft_revision,
        has_unpublished=latest is None
        or row.draft_revision != latest.source_revision,
        latest_version=latest.version if latest is not None else None,
        next_run_at=await _next_run_at(session, row.id),
        last_run=(
            LastRunOut(
                id=str(last.id),
                status=last.status,
                created_at=last.created_at,
            )
            if last is not None
            else None
        ),
        created_at=row.created_at,
    )


async def _detail(session: AsyncSession, row: Workflow) -> WorkflowDetailOut:
    latest = await _latest_version(session, row.id)
    base = await _summary(session, row)
    schedules = (
        (
            await session.execute(
                select(WorkflowSchedule)
                .where(WorkflowSchedule.workflow_id == row.id)
                .order_by(
                    WorkflowSchedule.created_at, WorkflowSchedule.id
                )
            )
        )
        .scalars()
        .all()
    )
    return WorkflowDetailOut(
        **base.model_dump(),
        draft_graph=row.draft_graph,
        latest_published=_version_out(latest) if latest is not None else None,
        schedules=[_schedule_out(item) for item in schedules],
    )


def _issues(result: ValidationResult) -> dict:
    return {"issues": [issue.as_dict() for issue in result.issues]}


def _check_savable(raw: dict) -> None:
    result = check_structure(raw)
    if not result.ok:
        raise AppError(
            "workflow_invalid", "图结构不合法", 422, detail=_issues(result)
        )


async def _validate(
    session: AsyncSession, user: User, raw: Any
) -> ValidationResult:
    return validate_publish(
        raw,
        registry=get_tool_registry(),
        skill_ids=set(get_skill_registry().skills),
        overrides=await load_overrides(session, user.tenant_id),
    )


@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(
    user: User = Depends(admin_access),  # noqa: B008
) -> list[TemplateOut]:
    return [
        TemplateOut(
            key=item.key,
            name=item.name,
            description=item.description,
            parameters=item.parameters,
        )
        for item in TEMPLATES
    ]


@router.post(
    "/templates/{key}/instantiate",
    response_model=WorkflowOut,
    status_code=201,
)
async def instantiate_template(
    key: str,
    payload: InstantiateIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowOut:
    template = get_template(key)
    if template is None:
        raise AppError("workflow_not_found", "模板不存在", 404)
    name = payload.name.strip()
    if not name:
        raise AppError("workflow_invalid", "名称不能为空", 422)
    row = Workflow(
        tenant_id=user.tenant_id,
        name=name,
        description=template.description,
        created_by=user.id,
        draft_graph=copy.deepcopy(template.graph),
        draft_revision=0,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.create",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"template": key},
    )
    return await _summary(session, row)


@router.post("", response_model=WorkflowOut, status_code=201)
async def create_workflow(
    payload: CreateIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowOut:
    name = payload.name.strip()
    if not name:
        raise AppError("workflow_invalid", "名称不能为空", 422)
    if payload.graph is not None:
        _check_savable(payload.graph)
    row = Workflow(
        tenant_id=user.tenant_id,
        name=name,
        description=payload.description,
        created_by=user.id,
        draft_graph=payload.graph,
        draft_revision=0,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.create",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
    )
    return await _summary(session, row)


@router.get("", response_model=list[WorkflowOut])
async def list_workflows(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[WorkflowOut]:
    rows = (
        (
            await session.execute(
                select(Workflow)
                .where(
                    Workflow.tenant_id == user.tenant_id,
                    Workflow.archived_at.is_(None),
                )
                .order_by(Workflow.created_at.desc(), Workflow.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [await _summary(session, row) for row in rows]


@router.get("/{workflow_id}", response_model=WorkflowDetailOut)
async def get_workflow(
    workflow_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowDetailOut:
    return await _detail(session, await _owned(session, workflow_id, user))


@router.patch("/{workflow_id}", response_model=WorkflowDetailOut)
async def update_workflow(
    workflow_id: str,
    payload: UpdateIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowDetailOut:
    row = await _owned(session, workflow_id, user)
    if payload.revision != row.draft_revision:
        raise AppError(
            "workflow_conflict", "草稿已被其他会话修改，请重新加载", 409
        )
    changed: list[str] = []
    updates: dict[str, Any] = {}
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise AppError("workflow_invalid", "名称不能为空", 422)
        updates["name"] = name
        changed.append("name")
    if payload.description is not None:
        updates["description"] = payload.description
        changed.append("description")
    if payload.draft_graph is not None:
        _check_savable(payload.draft_graph)
        updates["draft_graph"] = payload.draft_graph
        changed.append("graph")
    updates["draft_revision"] = Workflow.draft_revision + 1
    # 条件更新即 CAS：revision 不匹配时 rowcount 为 0，避免两个并发保存互相覆盖。
    saved = await session.execute(
        update(Workflow)
        .where(
            Workflow.id == row.id,
            Workflow.tenant_id == user.tenant_id,
            Workflow.archived_at.is_(None),
            Workflow.draft_revision == payload.revision,
        )
        .values(**updates)
        .execution_options(synchronize_session=False)
    )
    if saved.rowcount == 0:
        await session.rollback()
        raise AppError(
            "workflow_conflict", "草稿已被其他会话修改，请重新加载", 409
        )
    await session.commit()
    await session.refresh(row)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.update",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"fields": changed, "revision": row.draft_revision},
    )
    return await _detail(session, row)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: str,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> None:
    row = await _owned(session, workflow_id, user)
    active = (
        await session.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.workflow_id == row.id,
                WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
            )
            .limit(1)
        )
    ).first()
    if active is not None:
        raise AppError(
            "workflow_run_active", "工作流仍有活跃运行，无法删除", 409
        )
    row.archived_at = _now()
    await session.commit()
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.update",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"archived": True},
    )


@router.post("/{workflow_id}/validate", response_model=ValidateOut)
async def validate_workflow(
    workflow_id: str,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ValidateOut:
    row = await _owned(session, workflow_id, user)
    if row.draft_graph is None:
        return ValidateOut(
            ok=False,
            issues=[
                {
                    "code": "graph_invalid",
                    "message": "草稿为空",
                    "node_id": "",
                    "severity": "error",
                }
            ],
        )
    result = await _validate(session, user, row.draft_graph)
    return ValidateOut(**result.as_dict())


@router.post(
    "/{workflow_id}/publish", response_model=VersionOut, status_code=201
)
async def publish_workflow(
    workflow_id: str,
    payload: PublishIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> VersionOut:
    row = await _owned(session, workflow_id, user)
    if payload.revision != row.draft_revision:
        raise AppError(
            "workflow_conflict", "草稿已被其他会话修改，请重新加载", 409
        )
    if row.draft_graph is None:
        raise AppError("workflow_invalid", "草稿为空，无法发布", 422)
    result = await _validate(session, user, row.draft_graph)
    if not result.ok:
        raise AppError(
            "workflow_invalid", "工作流校验未通过", 422, detail=_issues(result)
        )
    latest = await _latest_version(session, row.id)
    version = WorkflowVersion(
        tenant_id=row.tenant_id,
        workflow_id=row.id,
        version=(latest.version + 1) if latest is not None else 1,
        graph=copy.deepcopy(row.draft_graph),
        source_revision=row.draft_revision,
        published_by=user.id,
        published_at=_now(),
        note=payload.note,
    )
    session.add(version)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError(
            "workflow_conflict", "有并发发布，请重试", 409
        ) from None
    await session.refresh(version)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.publish",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"version": version.version},
    )
    return _version_out(version)


@router.get("/{workflow_id}/versions", response_model=list[VersionOut])
async def list_versions(
    workflow_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[VersionOut]:
    row = await _owned(session, workflow_id, user)
    rows = (
        (
            await session.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == row.id)
                .order_by(WorkflowVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_version_out(item) for item in rows]


@router.get(
    "/{workflow_id}/versions/{version}",
    response_model=VersionDetailOut,
)
async def get_version(
    workflow_id: str,
    version: int,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> VersionDetailOut:
    row = await _owned(session, workflow_id, user)
    found = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == row.id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise AppError("workflow_not_found", "版本不存在", 404)
    return VersionDetailOut(
        **_version_out(found).model_dump(), graph=found.graph
    )


@router.post(
    "/{workflow_id}/versions/{version}/restore",
    response_model=WorkflowDetailOut,
)
async def restore_version(
    workflow_id: str,
    version: int,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowDetailOut:
    row = await _owned(session, workflow_id, user)
    found = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == row.id,
                WorkflowVersion.version == version,
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise AppError("workflow_not_found", "版本不存在", 404)
    row.draft_graph = copy.deepcopy(found.graph)
    row.draft_revision += 1
    await session.commit()
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="workflow.update",
        actor=user,
        resource_type="workflow",
        resource_id=str(row.id),
        detail={"restored": found.version},
    )
    return await _detail(session, row)
