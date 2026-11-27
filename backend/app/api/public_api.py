import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import agent as agent_api
from app.api import workflow_runs as workflow_runs_api
from app.db import get_session
from app.errors import AppError
from app.models.api_key import ApiKey
from app.models.user import User
from app.permissions import has_role
from app.queue import get_queue
from app.services.api_keys import authenticate_key
from app.services.audit import record_best_effort

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public", tags=["public-api"])


@dataclass
class ApiPrincipal:
    key: ApiKey
    user: User


def require_scope(
    scope: str, min_role: str
) -> Callable[..., Awaitable[ApiPrincipal]]:
    async def dependency(
        x_api_key: Annotated[
            str | None, Header(alias="X-API-Key")
        ] = None,
        authorization: Annotated[str | None, Header()] = None,
        session: AsyncSession = Depends(get_session),  # noqa: B008
    ) -> ApiPrincipal:
        secret = (x_api_key or "").strip()
        if not secret and (authorization or "").lower().startswith("bearer "):
            secret = authorization[7:].strip()  # type: ignore[union-attr]
        key, user = await authenticate_key(session, secret)
        if scope not in (key.scopes or []):
            raise AppError("api_key_scope_denied", "API Key 缺少所需权限", 403)
        if not has_role(user, min_role):  # type: ignore[arg-type]
            raise AppError("api_key_scope_denied", "API Key 所属成员权限不足", 403)
        return ApiPrincipal(key=key, user=user)

    return dependency


agent_run_scope = require_scope("agent:run", "editor")
agent_read_scope = require_scope("agent:read", "viewer")
workflow_run_scope = require_scope("workflow:run", "editor")
workflow_read_scope = require_scope("workflow:read", "viewer")


async def _audit_access(
    session: AsyncSession,
    principal: ApiPrincipal,
    request: Request,
    status: int,
    error_code: str = "",
) -> None:
    detail = {
        "endpoint": request.url.path,
        "method": request.method,
        "status": status,
        "key_prefix": principal.key.prefix,
        "key_name": principal.key.name,
    }
    if error_code:
        detail["error_code"] = error_code
    await record_best_effort(
        session,
        tenant_id=principal.key.tenant_id,
        action="api_key.access",
        actor=f"apikey:{principal.key.prefix}",
        resource_type="api_key",
        resource_id=str(principal.key.id),
        status="success" if status < 400 else "failed",
        detail=detail,
        request=request,
    )


@router.post("/agent/runs", response_model=agent_api.RunOut, status_code=202)
async def public_create_agent_run(
    payload: agent_api.RunIn,
    request: Request,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
    principal: ApiPrincipal = Depends(agent_run_scope),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> agent_api.RunOut:
    try:
        result = await agent_api.create_run(
            payload=payload,
            idempotency_key=idempotency_key,
            user=principal.user,
            session=session,
            queue=queue,
        )
    except AppError as exc:
        await _audit_access(session, principal, request, exc.status, exc.code)
        raise
    await _audit_access(session, principal, request, 202)
    return result


@router.get("/agent/runs/{run_id}", response_model=agent_api.RunDetailOut)
async def public_get_agent_run(
    run_id: str,
    request: Request,
    principal: ApiPrincipal = Depends(agent_read_scope),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> agent_api.RunDetailOut:
    try:
        result = await agent_api.get_run(
            run_id=run_id,
            limit=100,
            offset=0,
            user=principal.user,
            session=session,
        )
    except AppError as exc:
        await _audit_access(session, principal, request, exc.status, exc.code)
        raise
    await _audit_access(session, principal, request, 200)
    return result


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=workflow_runs_api.WorkflowRunOut,
    status_code=202,
)
async def public_start_workflow_run(
    workflow_id: str,
    payload: workflow_runs_api.StartIn,
    request: Request,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
    principal: ApiPrincipal = Depends(workflow_run_scope),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> workflow_runs_api.WorkflowRunOut:
    try:
        result = await workflow_runs_api.start_run(
            workflow_id=workflow_id,
            payload=payload,
            idempotency_key=idempotency_key,
            user=principal.user,
            session=session,
            queue=queue,
        )
    except AppError as exc:
        await _audit_access(session, principal, request, exc.status, exc.code)
        raise
    await _audit_access(session, principal, request, 202)
    return result


@router.get(
    "/workflow-runs/{run_id}", response_model=workflow_runs_api.WorkflowRunDetailOut
)
async def public_get_workflow_run(
    run_id: str,
    request: Request,
    principal: ApiPrincipal = Depends(workflow_read_scope),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> workflow_runs_api.WorkflowRunDetailOut:
    try:
        result = await workflow_runs_api.get_run(
            run_id=run_id, user=principal.user, session=session
        )
    except AppError as exc:
        await _audit_access(session, principal, request, exc.status, exc.code)
        raise
    await _audit_access(session, principal, request, 200)
    return result
