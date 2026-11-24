import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api import (
    accounts,
    agent,
    agent_team,
    analytics,
    audit,
    auth,
    calendar,
    chat,
    members,
    profiles,
    projects,
    publish,
    skills,
    tenants,
    workflow_runs,
    workflows,
)
from app.api import settings as settings_api
from app.config import get_settings
from app.db import SessionLocal
from app.errors import AppError, app_error_handler
from app.logging_setup import (
    RequestContextMiddleware,
    configure_http_logging,
)
from app.metrics import render_metrics
from app.seed import ensure_admin

configure_http_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with SessionLocal() as session:
        await ensure_admin(session)
    yield


async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    # 只保留 loc/msg/type，绝不回显 input/ctx，避免泄露密钥等原始输入
    detail = [
        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "请求参数校验失败",
            "detail": detail,
        },
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # 未预期异常只记录在服务端，响应体不携带堆栈或内部细节
    logger.error(
        "未处理异常 %s %s", request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": "服务器内部错误"},
    )


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)
app.include_router(auth.router, prefix="/api")
app.include_router(accounts.router, prefix="/api")
app.include_router(agent.router, prefix="/api")
app.include_router(agent_team.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(calendar.router, prefix="/api")
app.include_router(profiles.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(publish.router, prefix="/api")
app.include_router(skills.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(members.router, prefix="/api")
app.include_router(tenants.router, prefix="/api")
app.include_router(audit.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")
app.include_router(workflows.router, prefix="/api")
app.include_router(workflow_runs.router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint(request: Request) -> PlainTextResponse:
    # 令牌为空时视作未启用，返回 404 而不是暴露空指标端点
    token = get_settings().metrics_token.strip()
    if not token:
        return PlainTextResponse("not found\n", status_code=404)
    supplied = request.headers.get("x-metrics-token", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, token):
        return PlainTextResponse("unauthorized\n", status_code=401)
    return PlainTextResponse(render_metrics())
