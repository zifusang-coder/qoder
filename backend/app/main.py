import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import accounts, auth, chat, profiles, projects, publish, skills
from app.api import settings as settings_api
from app.config import get_settings
from app.db import SessionLocal
from app.errors import AppError, app_error_handler
from app.seed import ensure_admin

# httpx 默认在 INFO 级别记录完整请求 URL，公众号 access_token 会因此进入日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router, prefix="/api")
app.include_router(accounts.router, prefix="/api")
app.include_router(profiles.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(publish.router, prefix="/api")
app.include_router(skills.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
